"""The owner as a participant: questions to them, reminders, the "away" mode, decisions made without them.
The OWNER machine is in core/machines.py. Pure functions: time comes from event ts, the time zone from `owner.settings`
(hubd writes it from the settings).

A question to the owner is an open session question (`pending`) or a risky tool request (`needs-owner`).
Reminders: after 30 min, after 3 h, in the morning at 09:00; between 23 and 09 they move to 09:00; none while the owner
is away; after they return, not sooner than 30 min.
"""
import datetime
import functools
import hashlib
import json
import re
import urllib.parse

from core import coretext, forms, i18n, langdata
from core.coretext import T
from core.zones import zone

REMIND_AFTER = (30 * 60, 3 * 3600)  # the first and the second; the third is in the morning at 09:00
# a hub question handed over as a card (it does not hold the turn, 04.10): lives a day, then goes stale — no card, the answer gets a note
ASK_TTL = 24 * 3600
# the standard answer to any card (owner decision 2026-10-06: ask for more context, and the session then answers with
# screenshots, specs, mermaid diagrams as needed): a button in Telegram and on the page; sessions get a hint
MORE_CONTEXT = "Need more context"   # the English form; the page and Telegram send the label in the owner's language
# the answer earlier pages and Telegram bots sent (old journals, a bot process not restarted yet): the same answer
MORE_CONTEXT_OLD = langdata.legacy("more_context")


@functools.lru_cache(maxsize=1)
def more_forms():
    """Every form of the "more context" answer: the page and Telegram send the button's label in the owner's language
    (the session reads his words as he chose them), earlier ones the Russian one, a client the English one."""
    return frozenset({MORE_CONTEXT, MORE_CONTEXT_OLD, *(i18n.tr(lang, key) for lang in i18n.available()
                                                        for key in ("core.owner.more_context", "web.cards.more_context"))})


def asks_more(answers):
    """The owner's answers to a card ask for more context (in any language of the catalogs, or the earlier forms)."""
    return any(isinstance(v, str) and v in more_forms() for v in (answers or {}).values())


MORE_HINT = T("core.msg.more_hint",
              "The owner asks for more context. Answer right here, in detail, not with another reference: what we "
              "are deciding, why now, what happens under each option. Show, do not only retell: screenshots — `hub "
              "send-file <png> \"<what is on it>\"`, a diagram — a ```mermaid block in the answer (in Telegram it "
              "comes as a picture), a spec — its full path or `hub send-file`. They did not understand the card; "
              "the decision is still theirs. First, in the first lines — an example from life: who does what and "
              "what they see; not the card retold in the same words. Then in plain words, as to a person who "
              "does not know the topic. Then ask again the questions that got \"Need more context\" (do not re-ask the ones already "
              "answered): the same questions and the same options (an option may be added, none may be dropped), "
              "with the context in the question itself, without \"see below\". Making the card shorter or \"simpler\" "
              "for them — fewer questions, fewer options, a yes/no instead of a choice, a part of the decision "
              "\"I'll do it myself\" — changes the task, and that is also their decision.")


def question_key(ask):
    """The question's text without case and spacing: the same question asked again (duplicates across sessions, a stale
    question raised once)."""
    return " ".join(question_text(ask).lower().split())


def waits_return(ask):
    """The card was asked while the owner was away and they have not returned yet: it waits for them — no push or Telegram, no expiry."""
    return bool(ask.get("away")) and ask.get("back_at") is None


def ask_since(ask):
    """Since when the card has been in front of the owner: one asked without them counts from their return, one held for
    the working morning (quiet_review) from its release (expiry, reminders, push)."""
    return ask.get("back_at") or ask.get("held_until") or ask["since"]


def held(item, now):
    """A card or a tool request asked in non-working hours (hub.json quiet_review) is still waiting for the working
    morning at now: on the page, but no push, no Telegram, no reminders."""
    return bool(item.get("held_until")) and now < item["held_until"]


def before(ask, ts):
    """The card was in front of the owner before their words at ts: if they were present, asked no later than ts; if
    asked without them, shown before ts (the word they returned with is not an answer to a card they have not seen)."""
    if ask.get("away"):
        return ask.get("back_at") is not None and ask["back_at"] < ts
    return ask["since"] <= ts


def open_asks(sess, now=None):
    """Session questions to the owner handed over as a card: not yet answered and not stale at now (expiry counts from ask_since)."""
    return [a for a in sess.get("asks") or [] if now is None or waits_return(a) or now - ask_since(a) < ASK_TTL]
QUIET_FROM, QUIET_TO = 23, 9
BACK_GRACE = 30 * 60


def initial():
    return {"state": "present", "tz": "UTC", "away_since": None, "until": None, "back_at": None,
            "reminders": {}, "decided": [], "deferred": [], "done": []}


# --- questions ---------------------------------------------------------------------------------------------------

def questions(st, now=None):
    """Open questions to the owner: [(id, session, since when, text)]; a removed session has none."""
    out = []
    for name, s in st["sessions"].items():
        if s.get("state") == "removed":
            continue
        for req in (s.get("pending") or []) + open_asks(s, now):
            out.append((f"q:{name}:{req['request_id']}", name, ask_since(req), question_text(req)))
        for r in s.get("requests") or []:
            if r["state"] == "needs-owner":
                out.append((f"q:{name}:tool:{r['tool']}", name, r.get("held_until") or r["since"],
                            CONNECT_TOOL.format(tool=r["tool"], why=r.get("why") or "")))
    return out


def answer_sig(behavior, answers=None):
    """Fingerprint of the owner's decision on a card (allow/deny/dismissed and answers): the page uses it to recognize
    the same decision sent again (a client got no reply and retried, a second client) and to tell a different one apart."""
    body = {"behavior": str(behavior or ""),
            "answers": {str(k): str(v).strip() for k, v in (answers if isinstance(answers, dict) else {}).items()}}
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def question_head(req, limit=120):
    """The question's text for a message, cut to limit; the core's own words in it keep their marks when not cut."""
    q = question_text(req)
    return q if len(q) <= limit else q[:limit]


def question_text(req):
    if req.get("tool") == "AskUserQuestion":
        return " / ".join(q.get("question", "") for q in (req.get("input") or {}).get("questions", [])) or QUESTION
    return ALLOW_TOOL.format(tool=req.get("tool"))


FIRST_SENTENCE = re.compile(r"^([^\n]{1,200}?\?)(?=\s)")   # the question comes first, then what is behind it
FILE_LINK = re.compile(r"(?<![(\[])file:///[^\s<>)\]]*[^\s<>)\].,;:!?]")
DESIGN_LINK = re.compile(r"(?<![(\[])https://(?:www\.)?figma\.com/(\w+)/[^\s<>)\]]*[^\s<>)\].,;:!?]")


def _readable(question):
    """One question as Markdown: its first sentence is bold when it is a question and has something behind it (a lone
    question stays plain: all bold is no emphasis); a file and a design link are short (the long address is the target)."""
    text = FILE_LINK.sub(lambda m: f"[{urllib.parse.unquote(m[0].rstrip('/').rsplit('/', 1)[-1])}]({m[0]})", question.strip())
    text = DESIGN_LINK.sub(lambda m: "[" + i18n.t("core.owner.link_design" if m[1] in ("design", "file", "proto", "make")
                                                  else "core.owner.link_figma") + f"]({m[0]})", text)
    head = FIRST_SENTENCE.match(text)
    if "**" in text or not head or re.search(r"[.!]\s", head[1]) or not text[head.end():].strip():
        return text
    return f"**{head[1]}**{text[head.end():]}"


def question_md(req):
    """A card's questions for reading (reminders on the page and in Telegram): a paragraph each, where question_text glues
    them into one line with " / " (comparison, prompts). None for anything else: its one line reads as it is."""
    if req.get("tool") != "AskUserQuestion":
        return None
    parts = [_readable(q.get("question", "")) for q in (req.get("input") or {}).get("questions", [])]
    return "\n\n".join(p for p in parts if p) or None


def reminder_md(st, qid, now=None):
    """The Markdown of the open question qid (questions()), or None: not a card, or closed."""
    for name, s in st["sessions"].items():
        for req in (s.get("pending") or []) + open_asks(s, now):
            if f"q:{name}:{req['request_id']}" == qid:
                return question_md(req)
    return None


# the core's words in a question's text (core/coretext.py: the owner reads them in his language where they are shown)
CONNECT_TOOL = T("core.owner.connect_tool", "connect {tool}? {why}")
QUESTION = T("core.owner.question", "question")
ALLOW_TOOL = T("core.owner.allow_tool", "allow {tool}?")


# --- notification reasons (push spec, §2.1) ---------------------------------------------------------------------------
# A reason is what the hub can tell the phone about: a question and a tool request (from session state) and the
# owner.notify notification — the core remembers it in st["notices"]: without a record there is nothing to close the
# notification with or to give the client a list of open ones.
NOTICE_TTL = 24 * 3600   # a notification stays open a day from its last update
NOTICE_MAX = 50          # no more open ones; beyond that the oldest is closed (a parameter: there were 29 in a week)
NOTICE_ID = 128
NOTICE_TEXT = 2000       # the beginning is stored: the push needs <= 160 chars, Telegram takes the text from the event
DUE_MAX = 4e9            # a numeric deadline is seconds up to year 2096: beyond that time does not convert on all platforms


def notice_sender(ev, data):
    """Whose notification it is, by the labels set by put (bin/hub) and hubmod.fact: a module is mod:<name>, a plugin
    process is ext:<name> (its own name will not clash with core names), a schedule job is schedule:<job>, core parts
    (stats, watch, update) are their source, the core is core, otherwise the owner."""
    if data.get("channel") == "core":
        return "core"
    if data.get("channel") == "plugin" and data.get("sender"):   # module — mod:<name>, plugin process — ext:<name>
        return forms.outside_id(data["sender"])
    source = data.get("source")
    # a command job of the owner's (src cli) or of a session's (its name in HUB_SESSION, modules/scheduler/clock.py)
    if source and (ev.get("src") == "cli" or data.get("channel") == "session" and data.get("schedule_job") == source):
        return f"schedule:{source}"
    return str(source) if source else "owner"


def _notice_key(sender, nid):
    return f"n:{sender}:{nid}"


def notice_key(ev, data):
    """The full id n:<sender>:<id> of a notification by an owner.notify or owner.notify_resolved event (a full id n:… of
    the removal is taken as it is); None without an id: such a notification is a thing of its own."""
    nid = data.get("id")
    if not (isinstance(nid, str) and nid):
        return None
    return nid if nid.startswith("n:") else _notice_key(notice_sender(ev, data), nid)


def _prune_notices(notices, now):
    for k in [k for k, n in notices.items() if now - n["at"] >= NOTICE_TTL]:
        del notices[k]


def job_gone(st, job_id):
    """Schedule job job_id was removed or ran past its until: its notices (sender schedule:<job>) close with it. A
    milestone job is removed at the end of the work (hosts/streamjson.PROGRESS), and its last line stayed open for the
    phone until the day ran out (03.11). A one-shot job that has run (done) keeps its notice: that is its result."""
    notices = st.get("notices") or {}
    for key in [k for k, n in notices.items() if n.get("sender") == f"schedule:{job_id}"]:
        del notices[key]


def noticed(st, ev, data):
    """owner.notify accepted: open a notification or update the open one with the same id (v is the seq of the latest)."""
    from core.tools import Rejected
    nid, session, push = data.get("id"), data.get("session"), data.get("push", True)
    if nid is not None and not (isinstance(nid, str) and 0 < len(nid) <= NOTICE_ID and nid.strip() == nid
                                and not nid.startswith("n:")):
        raise Rejected(T("core.owner.notice_id",
                         "a notice id is a string of up to {max} characters, not starting with \"n:\" (that is how a full "
                         "id starts)").format(max=NOTICE_ID))
    if session is not None and (st["sessions"].get(session) or {}).get("state") in (None, "removed"):
        raise Rejected(T("core.mcpwatch.no_session", "no session {name}").format(name=str(session)[:40]))
    if not isinstance(push, bool):
        raise Rejected(T("core.owner.push_bool", "push — true or false"))
    urgency, due_at, topic = data.get("urgency"), data.get("due"), data.get("topic")
    if urgency not in (None, "now", "later"):
        raise Rejected(T("core.owner.urgency", "urgency — now or later"))
    if due_at is not None and not (isinstance(due_at, str) and 0 < len(due_at) <= 40 or
                                   isinstance(due_at, (int, float)) and not isinstance(due_at, bool)
                                   and 0 < due_at < DUE_MAX):
        raise Rejected(T("core.owner.due", "due — a term as a string of up to 40 characters or a time in seconds"))
    if topic is not None and not isinstance(topic, str):
        raise Rejected(T("core.owner.topic", "topic — a string"))
    sender = notice_sender(ev, data)
    about = data.get("message") if sender == "watch" else None   # watchdog: "message not delivered"
    notices = st.setdefault("notices", {})
    _prune_notices(notices, ev["ts"])
    key = _notice_key(sender, nid) if nid else f"n:{ev['seq']}"
    if about is not None and (notices.get(key) or {}).get("message") != about:   # the same open one — update; a new one — only about a waiting message
        waiting(st, about)
    old = notices.pop(key, None)   # back to the end: dict order is oldest to newest
    notices[key] = {"id": key, "sender": sender, "since": old["since"] if old else ev["ts"], "at": ev["ts"],
                    "v": ev["seq"], "title": str(data.get("title") or "")[:200],
                    "text": str(data.get("text") or "")[:NOTICE_TEXT], "urgent": bool(data.get("urgent")),
                    **({"topic": topic[:200]} if topic else {}), **({"urgency": urgency} if urgency else {}),
                    **({"due": due_at} if due_at is not None else {}),
                    **({"session": session} if session else {}), **({"push": False} if not push else {}),
                    **({"message": about} if about is not None else {})}
    while len(notices) > NOTICE_MAX:
        del notices[next(iter(notices))]


def _arrived(msg):
    """The "not delivered" alarm about message msg is wrong if it arrived or was withdrawn (by the owner, the hub, a
    session close, a hook). It is right while the message waits, and when the core gave up after all typing attempts
    (GAVE_UP): the core will not type it again by itself, and only the owner can restart the hub."""
    from core.machines import GAVE_UP
    return not (msg["state"] in ("queued", "typing") or msg["state"] == "failed" and msg.get("reason") == GAVE_UP)


def waiting(st, mid):
    """A new alarm about undelivered message mid is still valid. The watchdog judges by its own view of the journal —
    after a rotation it spends minutes catching up and sees the past (26.10: "not delivered" sent to the owner three
    minutes after delivery); only the core knows the queue. If the message is not in the core, the alarm has nothing to rest on."""
    from core.tools import Rejected
    msg = st["messages"].get(mid) if isinstance(mid, str) else None
    if msg is None or _arrived(msg):
        raise Rejected(T("core.owner.alarm_stale", "message {id} arrived or was withdrawn: the alarm is stale").format(
            id=str(mid)[:40]))


def settle(st):
    """A notification about a message (watchdog: "not delivered") stays while the alarm is valid: arrived or withdrawn
    means closed. If the message is not in the state (core.pruned removed it) we do not know how it ended: the
    notification stays until it is closed or expires. Open ones are few: one pass per event."""
    notices = st.get("notices")
    if not notices:
        return
    for key in [k for k, n in notices.items() if "message" in n and
                st["messages"].get(n["message"]) is not None and _arrived(st["messages"][n["message"]])]:
        del notices[key]


def resolved(st, ev, data):
    """owner.notify_resolved {id}: close a notification. Your own — by your own id; the hub and the owner — any, by the
    full id n:…; someone else's — rejected. Your own already closed (or never opened) — nothing: a module can withdraw
    without remembering whether it is open."""
    from core.tools import Rejected
    nid = data.get("id")
    if not (isinstance(nid, str) and 0 < len(nid) <= NOTICE_ID + 80):
        raise Rejected(T("core.owner.notice_id_str", "a notice id is a string"))
    from core import schedule
    hub = (st["sessions"].get(data.get("sender")) or {}).get("kind") == "hub"
    if data.get("channel") == "session" and not hub and schedule.from_own_job(st, data):
        anyone = False   # its own job's command withdraws the job's own notice (schedule:<job>)
    elif data.get("channel") == "session":
        if not hub:
            raise Rejected(T("core.owner.notice_hub_only", "a session does not close the owner's notices — only the hub"))
        anyone = True
    else:
        anyone = data.get("channel") != "plugin" and notice_sender(ev, data) == "owner"
    own = notice_sender(ev, data)
    key = nid if nid.startswith("n:") else _notice_key(own, nid)
    if not anyone and not key.startswith(f"n:{own}:"):
        raise Rejected(T("core.owner.notice_foreign", "someone else's notice: {who}, the hub or the owner can close it")
                       .format(who=key.split(":")[1]))
    (st.get("notices") or {}).pop(key, None)


# --- push text (push spec, §2.7) ---------------------------------------------------------------------------------
# One function for all reasons. Sources in order: the author's fields (topic, urgency, due of owner.notify; the form
# header) -> a fallback by rules. Urgency comes only from the author: rules cannot check that a deadline in the text
# belongs to this decision and is not cancelled. The summarizer (a model, off by default) replaces the topic and text in the push module.
PUSH_TITLE, PUSH_BODY = 40, 160
_MARKUP = re.compile(r"(?m)\*\*|__|~~|`+|^\s*(?:#{1,6}|>+)\s+|(?<!\w)\*(?=\S)|(?<=\S)\*(?!\w)")
_IDS = re.compile(r"\[hub:[^\]]*\]|(?<![\w-])[qdm]\d{3,}(?![\w-])")   # the hub's qN, dN, mN — event numbers
_LABEL = re.compile(r"(?i)^(?:summary|tl;?dr|" + langdata.alt("words", "topic_label") + r")\s*(?::|—|-|\.)?\s*")
# emoji and list markers at the start (minus numbers, $ and / — part of the meaning)
_LEAD = re.compile("^(?:[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u2B00-\u2BFF\u203C\u2049"
                   "\uFE0F\u200D•·]+\\s*|[-–—*]\\s+)+")
_SENT = re.compile(r"(?<=[.!?…])\s+")
# generic form labels are not a topic (a Claude Code header can be "Next", "Question"…)
GENERIC = {"summary", "tl;dr", "tldr", "question", "questions", "next", "next step", "decision", "choice", "ok", "confirm",
           "confirmation", "clarification", *langdata.words("words", "topic_generic")}


def _clean_part(text):
    t = _LEAD.sub("", text.strip())
    t, n = _LABEL.subn("", t)
    t = _LEAD.sub("", t) if n else t
    return t[:1].upper() + t[1:] if n else t


def push_clean(text):
    """Strips markup, ids, "Summary:"/"TL;DR" labels and emoji at the start of lines and sentences; whitespace collapsed."""
    t = _IDS.sub(" ", _MARKUP.sub("", str(text or "")))
    lines = [" ".join(_clean_part(s) for s in _SENT.split(" ".join(line.split()))) for line in t.splitlines()]
    t = " ".join(x for x in " ".join(lines).split())
    return re.sub(r"\s+([,.;:!?…)])", r"\1", re.sub(r"\(\s*\)", "", t)).strip()


def push_cut(text, limit):
    """Cut at a word, the "…" counts toward the limit; a word longer than the limit is cut at a character."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    word = head.rsplit(" ", 1)[0] if " " in head and text[limit - 1] != " " else head
    return word.rstrip(" ,;:—–-") + "…"


def _sentences(text):
    return [s for s in _SENT.split(text) if re.search(r"\w", s)]


def _title_of(text):
    first = (_sentences(text) or [""])[0]
    return push_cut(first.rstrip(".!:;… "), PUSH_TITLE)


def _body_of(text):
    """The first sentence and the sentence with "?" if together <= 160; otherwise the question (or the first), cut at a word."""
    sents = _sentences(text)
    if not sents:
        return ""
    ask = next((s for s in sents if "?" in s), None)
    if ask is None or ask == sents[0]:
        return push_cut(sents[0], PUSH_BODY)
    both = f"{sents[0]} {ask}"
    return both if len(both) <= PUSH_BODY else push_cut(ask, PUSH_BODY)


def _waited(seconds, lang=None):
    m = max(1, round(seconds / 60))
    return i18n.tr(lang, "web.push.minutes", count=m) if m < 60 else i18n.tr(lang, "web.push.hours", count=round(m / 60))


def _sender_label(sender):
    return sender.split(":", 1)[1] if sender.startswith(("mod:", "schedule:", "ext:")) else sender


def _due_text(due, tz):
    if isinstance(due, (int, float)) and not isinstance(due, bool):
        try:
            return _local(due, tz).strftime("%d.%m %H:%M")
        except (OverflowError, OSError, ValueError):   # the snapshot and pushes must not fail on someone else's number
            return ""
    return str(due)


def push_text(item, owner_state=None, lang=None):
    """Push text for a reason -> {title, subtitle?, body}. item is what the reason is built from: kind, session, req
    (a question or a permission), tool/why (a tool request), notice (a registry entry), qid, since, v (reminder number)."""
    session, kind, v = item.get("session"), item["kind"], item.get("v") or 0
    lang = lang or i18n.current()
    waits = i18n.tr(lang, "web.push.waits", session=session or "")
    if kind == "notice":
        n = item["notice"]
        label = _sender_label(n["sender"])
        topic = push_clean(n.get("topic") or n.get("title") or "")
        own = ":" not in n["sender"]   # the owner and core parts (core, stats, watch, update) have no prefix
        title = (topic or _title_of(push_clean(n.get("text"))) or label) if own else \
            f"{label}: {topic}" if topic else label
        lead = ""
        if n.get("urgency") == "now":
            when = _due_text(n["due"], (owner_state or {}).get("tz") or "UTC") if n.get("due") is not None else ""
            lead = (i18n.tr(lang, "web.push.now_due", due=when) if when else i18n.tr(lang, "web.push.now")) + " "
        elif n.get("urgency") == "later":
            lead = i18n.tr(lang, "web.push.later") + " "
        body = push_cut(lead + (push_clean(n.get("text")) or label), PUSH_BODY)
        return {"title": push_cut(title, PUSH_TITLE), **({"subtitle": session} if session else {}), "body": body}
    if kind == "tool":
        title = i18n.tr(lang, "web.push.connect", tool=item["tool"])
        body = push_cut(push_clean(item.get("why") or "") or title, PUSH_BODY)
    else:
        req = item["req"]
        if req.get("tool") == "AskUserQuestion":
            qs = (req.get("input") or {}).get("questions") or [{}]
            text = push_clean(qs[0].get("question") or "")
            header = push_clean(qs[0].get("header") or "")
            generic = header.strip(" :.!?").casefold() in GENERIC
            title = push_cut(header, PUSH_TITLE) if header and not generic else _title_of(text)
            body = _body_of(text)
            if len(qs) > 1 and body:
                more = i18n.tr(lang, "web.push.more", count=len(qs) - 1)
                body = push_cut(body, PUSH_BODY - len(more) - 1) + " " + more
        else:   # a permission: "Allow <tool>?" and the start of the command
            title = i18n.tr(lang, "web.push.allow", tool=req.get("tool"))
            inp = req.get("input") or {}
            what = next((inp[k] for k in ("command", "file_path", "url", "description") if isinstance(inp.get(k), str)),
                        req.get("why") or "")
            body = push_cut(" ".join(str(what).split()), PUSH_BODY)
        title, body = title or waits, body or waits
        title = push_cut(title, PUSH_TITLE)
    if v and owner_state is not None:   # a reminder: the same topic, "Waiting for an answer for <how long>: …"
        at = due({**owner_state, "state": "present", "back_at": None, "reminders": {item["qid"]: v - 1}},
                 item["qid"], item["since"], urgent(ask_texts(item["req"]) if item.get("req") else [item.get("why")]))
        body = push_cut(i18n.tr(lang, "web.push.reminder", time=_waited((at or item["since"]) - item["since"], lang),
                                body=body), PUSH_BODY)
    return {"title": title, **({"subtitle": session} if session else {}), "body": body}


def alerts(st, now, lang=None):
    """Open reasons at now: [{id, kind, session?, card?, title, subtitle?, body, since, v}] — questions and forms
    (pending, hub cards), tool requests, notifications (except push: false). v is the question's reminder number or the
    notification's seq: when it grows, the banner is replaced. A removed session is not here: its reasons are closed."""
    out, own = [], st["owner"]
    rem = own["reminders"]
    for name, s in st["sessions"].items():
        if s.get("state") == "removed":
            continue
        for req in (s.get("pending") or []) + open_asks(s, now):
            if waits_return(req) or held(req, now):
                continue   # the owner is away or it is non-working time: the card waits, the phone is not woken
            qid = f"q:{name}:{req['request_id']}"
            item = {"kind": "question", "session": name, "req": req, "qid": qid, "since": ask_since(req),
                    "v": rem.get(qid, 0)}
            out.append({"id": qid, "kind": "question", "session": name, **({"card": req["id"]} if req.get("id") else {}),
                        **push_text(item, own, lang), "since": ask_since(req), "v": rem.get(qid, 0),
                        **({"quiet": True} if req.get("held_until") else {})})   # one of the morning batch (web/push)
        for r in s.get("requests") or []:
            if r["state"] == "needs-owner" and not held(r, now):
                qid = f"q:{name}:tool:{r['tool']}"
                since = r.get("held_until") or r["since"]
                item = {"kind": "tool", "session": name, "tool": r["tool"], "why": r.get("why"), "qid": qid,
                        "since": since, "v": rem.get(qid, 0)}
                out.append({"id": qid, "kind": "tool", "session": name, **push_text(item, own, lang),
                            "since": since, "v": rem.get(qid, 0), **({"quiet": True} if r.get("held_until") else {})})
    for n in (st.get("notices") or {}).values():
        if n.get("push") is False or now - n["at"] >= NOTICE_TTL:
            continue
        sess = n.get("session") if (st["sessions"].get(n.get("session")) or {}).get("state") not in (None, "removed") \
            else None
        out.append({"id": n["id"], "kind": "notice", **({"session": sess} if sess else {}),
                    **push_text({"kind": "notice", "session": sess, "notice": n}, own, lang),
                    "since": n["since"], "v": n["v"], "sender": n["sender"], "urgent": n["urgent"]})
    return out

# --- card text in the owner's words ----------------------------------------------------------------------------
# The owner copied a card (page, Telegram) into a message and added an answer ("<question> go"): the verbatim text of
# one open card's question means the rest of their words answer it. Only verbatim and only one card: a paraphrase
# (06.10, a paragraph from a hub reply) and anything ambiguous is decided by the hub. A short question ("Publish?") is not evidence.
QUOTE_MIN = 20
_MARKS = "*_`~"   # question markup that is absent from the copied card
HINT_KEYS = ("telegram.ask_own", "telegram.ask_multi_hint")   # the bot's hints under a card, in its language


@functools.lru_cache(maxsize=1)
def _card_forms():
    """The card's title (with {name}) and the hints, in every language a catalog has — read once per process: the fold
    does not list the catalog folder per message, and a re-fold of a journal sees the same forms."""
    langs = i18n.available()
    return (tuple(dict.fromkeys(i18n.tr(lang, "core.owner.card_asks", name="\0") for lang in langs)),
            tuple(dict.fromkeys(i18n.tr(lang, key) for key in HINT_KEYS for lang in langs)))


def card_pieces(name):
    """What the bot adds to a card's text (its title and hints) in every language a catalog has: the owner copied it
    in the language his bot spoke."""
    heads, hints = _card_forms()
    return list(dict.fromkeys([h.replace("\0", str(name)) for h in heads] + list(hints)))


def _words(text):
    """Words of the text without markup, case-insensitive: [(word as is, comparable)]; line breaks are None."""
    out, plain = [], str.maketrans("", "", _MARKS)
    for line in str(text or "").splitlines():
        out += [(w, w.translate(plain).casefold()) for w in line.split() if w.translate(plain)]
        out.append(None)
    return out[:-1]


def _find(words, piece):
    """Where in words the words of piece stand consecutively (across line breaks): indexes or []."""
    want = [k for _, k in (w for w in _words(piece) if w)]
    pos = [i for i, w in enumerate(words) if w]
    for s in range(len(pos) - len(want) + 1) if want else ():
        if all(words[pos[s + j]][1] == want[j] for j in range(len(want))):
            return pos[s:s + len(want)]
    return []


def quoted_card(st, text, now, only=None):
    """(session, card, their words) — the owner's message contains the verbatim question text of exactly one open card
    and something else; otherwise None. only — look only at this session (they wrote to it, not to the hub)."""
    words = _words(text)
    found = []
    for name, sess in st["sessions"].items():
        if only and name != only or not only and sess.get("kind") == "hub":
            continue
        for ask in open_asks(sess, now):
            if ask.get("hubbus"):   # a card of the hub bus is answered by its buttons only (core/busstate.card_answered)
                continue
            qs = [q.get("question", "") for q in (ask.get("input") or {}).get("questions") or []]
            if any(len(" ".join(q.split())) >= QUOTE_MIN and _find(words, q) for q in qs):
                found.append((name, ask))
    if len(found) != 1:
        return None
    name, ask = found[0]
    # remove what was copied: questions, the card title, options with explanations, bot hints
    pieces = card_pieces(name)
    for q in (ask.get("input") or {}).get("questions") or []:
        pieces.append(q.get("question", ""))
        pieces += [f"• {o.get('label', '')} — {o['description']}" for o in q.get("options") or [] if o.get("description")]
    drop = set()
    for p in pieces:
        while True:
            hit = _find([None if i in drop else w for i, w in enumerate(words)], p)
            if not hit:
                break
            drop.update(hit)
    lines, cur = [], []
    for i, w in enumerate(words + [None]):
        if w is None:
            lines.append(" ".join(cur))
            cur = []
        elif i not in drop and w[0] not in ("❓", "•"):
            cur.append(w[0])
    rest = "\n".join(lines).strip(" \n—–-:,.;")
    return (name, ask, rest) if rest.strip() else None


def cards_note(st, mid, now, hub=None):
    """For the hub, attached to the owner's message mN: the open session cards with numbers. Their words may answer one
    of them (07.10: a short "roll it out" with two cards open) — which one is for the hub to decide, the core does not guess."""
    cards = [(name, a) for name, s in st["sessions"].items() if name != hub for a in open_asks(s, now)]
    if not cards:
        return None
    lines = [(CARD_LINE_SHOWN if hub and a.get("shown") == hub else CARD_LINE).format(
        id=a["id"], name=name, question=" ".join(question_text(a).split())[:100]) for name, a in cards[-10:]]
    # the state keeps plain text (msg cards): only the hub's prompt reads it
    return "\n\n" + str(CARDS_NOTE.format(lines="; ".join(lines), mid=mid))


CARD_LINE = T("core.owner.card_line", "{id} at {name} — \"{question}\"")
CARD_LINE_SHOWN = T("core.owner.card_line_shown", "{id} at {name} (already in your chat) — \"{question}\"")
CARDS_NOTE = T("core.owner.cards_note",
               "(Open cards for the owner: {lines}. Their words are the answer to one of them: hub send <session> --for "
               "{mid} --card <qN> \"<their words>\" — the card closes, the session's result goes to them (for a new task "
               "from their words — hub task new <session> --for {mid} --card <qN> \"…\"). Not an answer — without "
               "--card: the card stays open. To show it to the owner here, in your chat — hub question show <qN>, not "
               "asking the question again: there is one card, their answer goes to the session.)")


# --- reminders -----------------------------------------------------------------------------------------------------

def _local(ts, tz):
    return datetime.datetime.fromtimestamp(ts, zone(tz))


def _next_morning(ts, tz):
    t = _local(ts, tz)
    morning = t.replace(hour=QUIET_TO, minute=0, second=0, microsecond=0)
    if t >= morning:
        morning += datetime.timedelta(days=1)
    return morning.timestamp()


def _out_of_quiet(ts, owner):
    at = _out_of_night(ts, owner["tz"])
    return review_end(owner, at) or at   # non-working hours (quiet_review): to the working morning


def _out_of_night(ts, tz):
    h = _local(ts, tz).hour
    return _next_morning(ts, tz) if (h >= QUIET_FROM or h < QUIET_TO) else ts


# --- non-working hours (hub.json quiet_review) ----------------------------------------------------------------------
# What waits for the owner's decision (cards, tool requests, their reminders) does not go to Telegram or
# the phone on a non-working day or after the working day: it piles up on the page and comes as one batch at the start of
# the next working day. The days off are Saturday and Sunday.
REVIEW = {"from": "19:00", "to": "10:00"}
REVIEW_LOOKAHEAD = 60        # the search for the next working morning
LIVE_WORD = 2 * 3600         # a card in a turn on the owner's word said no longer ago than this goes at once: he is in the conversation
URGENT_MARK = "🚨"           # a question beginning with it goes at once


def _hm(value):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip()) if isinstance(value, str) else None
    return (int(m[1]), int(m[2])) if m else None


def review_hours(value):
    """hub.json quiet_review → {"from", "to"} ("HH:MM") or None (off): true is 19:00–10:00, an object sets its own hours
    (a wrong one is the default)."""
    if value is True:
        return dict(REVIEW)
    if not isinstance(value, dict):
        return None
    out = {}
    for k in ("from", "to"):
        hm = _hm(value.get(k))
        out[k] = f"{hm[0]:02d}:{hm[1]:02d}" if hm else REVIEW[k]
    return out


def review_of(value):
    """owner.settings quiet_review → the owner's review state {from, to} or None (off)."""
    return review_hours(value)


def day_off(day):
    """A non-working day: Saturday and Sunday."""
    return day.weekday() >= 5


def review_end(owner, ts):
    """ts is in the owner's non-working hours (quiet_review): the whole day off, or from `from` to `to` of a working day →
    the moment it ends, `to` of the next working day; otherwise (or off) None."""
    rv = owner.get("review")
    if not rv:
        return None
    t = _local(ts, owner.get("tz"))
    fh, th = _hm(rv["from"]) or (19, 0), _hm(rv["to"]) or (10, 0)
    hm = (t.hour, t.minute)
    if not day_off(t.date()):
        inside = (hm >= fh or hm < th) if fh > th else fh <= hm < th
        if not inside:
            return None
    tz = zone(owner.get("tz"))
    for i in range(REVIEW_LOOKAHEAD):
        d = t.date() + datetime.timedelta(days=i)
        at = datetime.datetime(d.year, d.month, d.day, *th, tzinfo=tz).timestamp()
        if at > ts and not day_off(d):
            return at
    return None


def review_hold(st, ts, texts, names):
    """Until when to hold a card or a tool request at ts (its texts; names — the sessions whose turn it comes from), or
    None — it goes at once: the setting is off; the owner is away (his mode is stronger: the card waits for his return);
    it is working time; a question beginning with 🚨; the turn is on the owner's live word (he is in the conversation)."""
    o = st["owner"]
    if not o.get("review") or o["state"] == "away":
        return None
    if urgent(texts):
        return None
    if any(live_word(st, n, ts) for n in names if n):
        return None
    return review_end(o, ts)


def urgent(texts):
    """A question marked urgent: one of its texts begins with 🚨 (it is not held, and its reminders are not either)."""
    return any(str(t or "").lstrip().startswith(URGENT_MARK) for t in texts)


def live_word(st, name, ts):
    """Session name is running a turn that started from the owner's message no older than LIVE_WORD, not through a
    schedule firing."""
    from core import chain
    sess = (st.get("sessions") or {}).get(name) or {}
    m = (st.get("messages") or {}).get(chain.turn_root(st, name) or "") or {}
    return bool(m.get("from_owner")) and ts - (m.get("created") or 0) <= LIVE_WORD \
        and not chain.of_turn_scheduled(st, sess.get("turn_marker"), sess.get("turn_also") or ())


def ask_texts(req):
    """The question texts of a card (each question of the form) — for the 🚨 mark."""
    qs = [q.get("question") for q in (req.get("input") or {}).get("questions") or [] if isinstance(q, dict)]
    return qs or [question_text(req)]


def review_release(st, ts):
    """quiet_review turned off: what is held goes now (its deadline counts from now)."""
    for s in st["sessions"].values():
        for item in (s.get("asks") or []) + (s.get("requests") or []):
            if held(item, ts):
                item["held_until"] = ts


def due(owner, qid, since, loud=False):
    """When the next reminder for the question is due, or None (all three sent / the owner is away). loud — an urgent
    card (🚨): only the night moves its reminder, not the non-working hours."""
    if owner["state"] == "away":
        return None
    n = owner["reminders"].get(qid, 0)
    tz = owner["tz"]
    if n < len(REMIND_AFTER):
        at = since + REMIND_AFTER[n]
    elif n == len(REMIND_AFTER):
        at = _next_morning(since + REMIND_AFTER[-1], tz)
    else:
        return None
    at = _out_of_night(at, tz) if loud else _out_of_quiet(at, owner)
    if owner.get("back_at"):
        at = max(at, owner["back_at"] + BACK_GRACE)
    return at


def loud_questions(st):
    """The ids of the open urgent questions (🚨: cards, forms, tool requests by their why), as in questions()."""
    out = set()
    for name, s in st["sessions"].items():
        out.update(f"q:{name}:{a['request_id']}" for a in (s.get("pending") or []) + (s.get("asks") or [])
                   if urgent(ask_texts(a)))
        out.update(f"q:{name}:tool:{r['tool']}" for r in s.get("requests") or [] if urgent([r.get("why")]))
    return out


def pending(st):
    owner = st.get("owner") or initial()
    out = []
    loud = loud_questions(st) if owner.get("review") else set()
    for qid, _name, since, _text in questions(st):
        at = due(owner, qid, since, qid in loud)
        if at is not None:
            out.append((at, qid, owner["reminders"].get(qid, 0) + 1))
    if owner["state"] == "away" and owner.get("until"):
        out.append((owner["until"], "owner", None))
    return out


def remind(st, ev):
    """A reminder came due: record it (the channel will show/send it); if the question is already closed — nothing."""
    owner = st["owner"]
    qid, n = ev.get("entity"), (ev.get("data") or {}).get("n")
    open_ids = {q[0] for q in questions(st)}
    if qid not in open_ids or owner["state"] == "away" or n != owner["reminders"].get(qid, 0) + 1:
        return None
    owner["reminders"][qid] = n
    return next(q for q in questions(st) if q[0] == qid)


# --- away / back --------------------------------------------------------------------------------------------------

MAX_AWAY_HOURS = 24 * 30   # the same limit the page and Telegram check


def went_away(owner, ev, data):
    new_period(owner, ev, data)
    owner["away_since"] = ev["ts"]
    owner["decided"], owner["deferred"], owner["done"] = [], [], []


def new_period(owner, ev, data):
    """The end of the absence: `hours` from now, or none (until he returns). "Away" again while away does only this:
    the start of the absence and what piled up since then stay for the summary on return."""
    hours = data.get("hours")
    # check before any change: a NaN or infinite period breaks the timer, a past one is not an absence
    if hours and (isinstance(hours, bool) or not isinstance(hours, (int, float))
                  or not 0 < hours <= MAX_AWAY_HOURS):
        from core.tools import Rejected
        raise Rejected(T("core.owner.away_hours", "away for {hours} h: the number of hours must be above 0 and at most "
                                                  "{max}").format(hours=str(hours)[:20], max=MAX_AWAY_HOURS))
    owner["until"] = ev["ts"] + hours * 3600 if hours else None
    owner["away_seq"] = ev["seq"]   # which "away" set the current period (Telegram confirms only that one)


def came_back(st, ev):
    """Summary on return: the hub decided for you — check / waiting for your decision / sessions decided themselves /
    done. An open card comes with its number (the page links to it)."""
    owner = st["owner"]
    cards = {f"q:{name}:{a['request_id']}": a["id"] for name, s in st["sessions"].items()
             for a in open_asks(s, ev["ts"])}
    # the hub's answers to cards for him (spec d2549241): their own section — the session that asked and the line; only
    # when there are some (the summary of an absence without them stays as it was)
    hub = [{"session": d.get("asker"), "text": d["text"]} for d in owner["decided"] if d.get("hub")]
    summary = {
        "from": owner["away_since"], "to": ev["ts"],
        "decided": [d for d in owner["decided"] if not d.get("hub")],
        **({"hub": hub} if hub else {}),
        # the core's words of a question ("allow X?") with their marks: the owner reads them in his language (views)
        "waiting": [coretext.put({"session": q[1], **({"card": cards[q[0]]} if q[0] in cards else {})}, "text", q[3])
                    for q in questions(st)] + list(owner["deferred"]),
        "done": list(owner["done"]),
    }
    owner.update(away_since=None, until=None, back_at=ev["ts"])
    return summary
